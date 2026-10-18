"""PC のログイン（DESIGN.md 5-4・11章）。持ち主 CL-2。

2026-10-18 ユーザー：「SalesCall AIのログインとパスワードにより、データベースを判断できないの？」「グーグルIDでログインしたり、
パスキーを設定したりもできますか？」「はい、それで進めてください」

- **契約者が使うのは、SalesCall AI のログインだけ**（ID とパスワード・Google・パスキー）。PC は、パスワードに一切触れない：
  `login` がブラウザで SalesCall AI のログインのページを開き、確かめのコードを出す。契約者がそのページでログインしてコードを
  確かめると、サーバーが、その PC のデータベースの鍵を渡す（テレビの動画アプリに、スマホでログインするのと同じ形＝RFC 8628 の
  「機器のログイン」）。ログインのページとサーバーは CL-3（サーバーの入口の形は、下の `START`・`TOKEN`）。
- **受け取った鍵は、PC の中にしまう**（`save`。Windows は、その Windows の利用者だけが開ける形にしてから書く＝DPAPI。画面・記録に
  出さない）。見守り役は、`SALESCALL_DSN` が無ければ、しまった鍵でつなぐ（`load`。読むのは supervisor.config の1か所）。
- **契約者は命令を打たない**（2026-10-18 ユーザー：「普通はこんなこと一般の人がしないですよね。ふつうは、指定URLにとんでそこからログイン
  するだけでしょ」）：見守り役が動き出したとき（ログオンで自動）、まだログインしていなければ、`until_logged_in` が自動でブラウザに
  ログインのページを開き、ログインが済むまで待つ（済んだら、そのまま動き出す）。ログインしないまま期限が切れたら、新しいコードで
  待ち直す（ブラウザを開き直すのは `REOPEN_AFTER` ごとまで＝何度も開いて邪魔をしない）。命令 `login` は、管理者が手で直すとき用。
- 同じ PC でログインし直すと、前の鍵は止まる（サーバーへ、前の PC の番号を渡す）。`logout` は、しまった鍵を消す（鍵そのものを
  止めるのは、画面の「この PC のログインを外す」）。
- サーバーとのやり取りは HTTPS だけ（鍵を運ぶため）。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

START = "/pc-login/start"    # 機器のログインを始める（返す：device_code・user_code・verification_uri(_complete)・interval・expires_in）
TOKEN = "/pc-login/token"    # 鍵を受け取る（返す：待って authorization_pending／遅く slow_down／断られた access_denied／時間切れ
                             #   expired_token、または tenant_id・pc・conninfo）


REOPEN_AFTER = 30 * 60      # ログインしないまま期限が切れたとき、ブラウザを開き直すまでの秒数（その間は、開いたページで待つ）
RETRY_AFTER = 60            # サーバーに届かない・断られたときに、待ち直すまでの秒数


class LoginFailed(Exception):
    """PC のログインができなかった（理由は日本語。鍵の値は書かない）。"""


@dataclass(frozen=True)
class PcKey:
    """この PC のデータベースの鍵。"""
    tenant_id: UUID
    pc: str
    conninfo: str

    def __repr__(self) -> str:   # 記録や誤りの表示に、鍵を出さない
        return f"PcKey(tenant_id={self.tenant_id!r}, pc={self.pc!r}, conninfo=***)"


def login(server_url: str, *, previous: PcKey | None = None, post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
          open_browser: Callable[[str], Any] = webbrowser.open, say: Callable[[str], Any] = print,
          sleep: Callable[[float], Any] = time.sleep, now: Callable[[], float] = time.monotonic) -> PcKey:
    """ブラウザでログインしてもらい、この PC の鍵を受け取る（しまうのは呼んだ側＝`save`）。previous＝この PC が前に持っていた鍵
    （ログインし直したとき。サーバーが前の鍵を止める）。"""
    if not server_url.startswith("https://"):
        raise LoginFailed("SalesCall AI のサーバーの場所は https:// で始まるものだけです（鍵を運ぶため）")
    post = post or _post
    base = server_url.rstrip("/")
    start = post(base + START, {"pc_label": socket.gethostname()})
    page = start.get("verification_uri_complete") or start["verification_uri"]
    say(f"ブラウザで SalesCall AI のログインのページを開きます（開かないときは、このページを開いてください：{page}）。\n"
        f"ページに出る確かめのコードが「{start['user_code']}」と同じかを確かめてから、ログインしてください"
        "（ID とパスワード・Google・パスキーのどれでも）。")
    open_browser(page)
    interval, deadline = float(start.get("interval", 5)), now() + float(start.get("expires_in", 600))
    body = {"device_code": start["device_code"], **({"replaces": previous.pc} if previous else {})}
    while now() < deadline:
        sleep(interval)
        got = post(base + TOKEN, body)
        error = got.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise LoginFailed("ログインが取り消されました。もう一度、命令を打ってください")
        if error == "expired_token":
            break
        if error is not None:
            raise LoginFailed(f"サーバーが断りました（{error}）")
        return PcKey(UUID(got["tenant_id"]), str(got["pc"]), str(got["conninfo"]))
    raise LoginFailed("時間切れです（ログインが済まないうちに、確かめのコードの期限が切れました）。もう一度、命令を打ってください")


def until_logged_in(server_url: str, *, stop: Any = None, login_once: Callable[..., PcKey] | None = None,
                    open_browser: Callable[[str], Any] = webbrowser.open,
                    say: Callable[[str], Any] = print, sleep: Callable[[float], Any] = time.sleep,
                    now: Callable[[], float] = time.monotonic, path: Path | None = None) -> PcKey | None:
    """見守り役が動き出したときに、まだログインしていなければ呼ぶ：ログインが済むまで待ち直し、鍵をしまって返す（止める印が立てば
    None）。ブラウザを開くのは、初めてと、そのあと REOPEN_AFTER ごとまで。"""
    login_once = login_once or login
    opened_at: float | None = None
    while stop is None or not stop.is_set():
        reopen = opened_at is None or now() - opened_at >= REOPEN_AFTER
        try:
            key = login_once(server_url, previous=load(path), open_browser=open_browser if reopen else _quiet,
                             say=say if reopen else _quiet)
        except LoginFailed as e:
            if reopen:
                opened_at = now()
                say(f"{e}（しばらくしてから、もう一度ログインのページを開きます）")
            sleep(RETRY_AFTER)
            continue
        except OSError:   # サーバーに届かない（ネットがまだつながっていない など）：待ち直す
            sleep(RETRY_AFTER)
            continue
        save(key, path)
        return key
    return None


def _quiet(_: str) -> None:
    """開き直さない回は、ブラウザも案内も出さない（前に開いたページで待っている）。"""


def key_path() -> Path:
    """しまう場所（その Windows の利用者のフォルダ。ほかの OS は利用者のフォルダ）。"""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "SalesCall" / "pc-key.bin"
    return Path.home() / ".config" / "salescall" / "pc-key.bin"


def save(key: PcKey, path: Path | None = None) -> None:
    """鍵をしまう（書きかけを残さない。Windows はその利用者だけが開ける形にして、ほかの OS は本人だけが読める許しで）。"""
    path = path or key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _protect(json.dumps({"tenant_id": str(key.tenant_id), "pc": key.pc, "conninfo": key.conninfo}).encode("utf-8"))
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    tmp.replace(path)


def load(path: Path | None = None) -> PcKey | None:
    """しまった鍵（無ければ None）。"""
    path = path or key_path()
    if not path.exists():
        return None
    d = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
    return PcKey(UUID(d["tenant_id"]), d["pc"], d["conninfo"])


def logout(path: Path | None = None) -> bool:
    """しまった鍵を消す。消したら True。"""
    path = path or key_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    """JSON を送り、JSON を受け取る（断り＝400 の答えも、中身の error を返す）。"""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise LoginFailed(f"サーバーの答えが読めません（{e.code}）") from None


def _protect(data: bytes) -> bytes:
    """Windows：その利用者だけが開ける形にする（DPAPI）。ほかの OS：そのまま（ファイルの許しで守る）。"""
    return _dpapi(data, protect=True) if os.name == "nt" else data


def _unprotect(data: bytes) -> bytes:
    return _dpapi(data, protect=False) if os.name == "nt" else data


def _dpapi(data: bytes, *, protect: bool) -> bytes:   # pragma: no cover（Windows だけ）
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise LoginFailed("PC の鍵をしまう・読むことができませんでした（Windows の利用者が違う恐れ）")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)
