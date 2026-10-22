"""PC のログイン（PC の部品。DESIGN.md 5-5・11章）。持ち主 CL-2。

2026-10-18 ユーザー：「SalesCall AIのログインとパスワードにより、データベースを判断できないの？」「グーグルIDでログインしたり、
パスキーを設定したりもできますか？」「はい、それで進めてください」

- **契約者が使うのは、SalesCall AI のログインだけ**（ID とパスワード・Google・パスキー）。PC は、パスワードに一切触れない：
  `login` がブラウザで SalesCall AI のログインのページを開き、確かめのコードを出す。契約者がそのページでログインしてコードを
  確かめると、サーバーが、その PC の通行証を渡す（テレビの動画アプリに、スマホでログインするのと同じ形＝RFC 8628 の
  「機器のログイン」）。ログインのページとサーバーは CL-3（サーバーの入口の形は、下の `START`・`TOKEN`）。
- **受け取った通行証は、PC の中にしまう**（`save`。Windows は、その Windows の利用者だけが開ける形にしてから書く＝DPAPI。画面・記録に
  出さない）。PC の部品は、しまった通行証でサーバーにつなぐ（`load`。PC はデータベースにつながない＝2026-10-21 ユーザー承認の分け方）。
- **契約者は命令を打たない**（2026-10-18 ユーザー：「普通はこんなこと一般の人がしないですよね。ふつうは、指定URLにとんでそこからログイン
  するだけでしょ」）：PC の部品が動き出したとき（ログオンで自動）、まだログインしていなければ、`until_logged_in` が自動でブラウザに
  ログインのページを開き、ログインが済むまで待つ（済んだら、そのまま動き出す）。ログインしないまま期限が切れたら、新しいコードで
  待ち直す（ブラウザを開き直すのは `REOPEN_AFTER` ごとまで＝何度も開いて邪魔をしない）。命令 `login` は、管理者が手で直すとき用。
- 同じ PC でログインし直すと、前の通行証は止まる（サーバーへ、前の PC の番号を渡す）。`logout` は、しまった通行証を消す（通行証
  そのものを止めるのは、画面の「この PC のログインを外す」）。
- サーバーとのやり取りは HTTPS だけ（通行証を運ぶため）。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from salescall.link.basics import PcPass
from salescall.link.pass_file import load, logout, pass_path, save  # noqa: F401（しまう場所の決まりは link の1か所）

START = "/pc-login/start"    # 機器のログインを始める（返す：device_code・user_code・verification_uri(_complete)・interval・expires_in）
TOKEN = "/pc-login/token"    # 通行証を受け取る（返す：待って authorization_pending／遅く slow_down／断られた access_denied／時間切れ
                             #   expired_token、または tenant_id・pc・token。サーバーは core.agents.issue を呼ぶ）


REOPEN_AFTER = 30 * 60      # ログインしないまま期限が切れたとき、ブラウザを開き直すまでの秒数（その間は、開いたページで待つ）
RETRY_AFTER = 60            # サーバーに届かない・断られたときに、待ち直すまでの秒数


class LoginFailed(Exception):
    """PC のログインができなかった（理由は日本語。通行証の値は書かない）。"""


def login(server_url: str, *, previous: PcPass | None = None, post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
          open_browser: Callable[[str], Any] = webbrowser.open, say: Callable[[str], Any] = print,
          sleep: Callable[[float], Any] = time.sleep, now: Callable[[], float] = time.monotonic) -> PcPass:
    """ブラウザでログインしてもらい、この PC の通行証を受け取る（しまうのは呼んだ側＝`save`）。previous＝この PC が前に持っていた
    通行証（ログインし直したとき。サーバーが前の通行証を止める）。"""
    if not server_url.startswith("https://"):
        raise LoginFailed("SalesCall AI のサーバーの場所は https:// で始まるものだけです（通行証を運ぶため）")
    post = post or _post
    base = server_url.rstrip("/")
    try:   # サーバーの答えの形が違えば、ログインできなかったとして扱う（PC の部品を終わらせない）
        start = post(base + START, {"pc_label": socket.gethostname()})
        page = start.get("verification_uri_complete") or start["verification_uri"]
        code, device = str(start["user_code"]), str(start["device_code"])
        interval, deadline = float(start.get("interval", 5)), now() + float(start.get("expires_in", 600))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise LoginFailed("サーバーの答えの形が違います（ログインを始められませんでした）") from None
    say(f"ブラウザで SalesCall AI のログインのページを開きます（開かないときは、このページを開いてください：{page}）。\n"
        f"ページに出る確かめのコードが「{code}」と同じかを確かめてから、ログインしてください"
        "（ID とパスワード・Google・パスキーのどれでも）。")
    open_browser(page)
    body = {"device_code": device, **({"replaces": str(previous.pc_id)} if previous else {})}
    while now() < deadline:
        sleep(interval)
        got = post(base + TOKEN, body)
        error = got.get("error") if isinstance(got, dict) else "invalid_response"
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise LoginFailed("ログインが取り消されました（しばらくすると、ログインのページがまた開きます）")
        if error == "expired_token":
            break
        if error is not None:
            raise LoginFailed(f"サーバーが断りました（{error}）")
        try:
            return PcPass(UUID(got["tenant_id"]), UUID(got["pc"]), str(got["token"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise LoginFailed("サーバーの答えの形が違います（通行証を受け取れませんでした）") from None
    raise LoginFailed("時間切れです（ログインが済まないうちに、確かめのコードの期限が切れました。しばらくすると、ログインのページが"
                      "また開きます）")


def until_logged_in(server_url: str, *, stop: Any = None, login_once: Callable[..., PcPass] | None = None,
                    open_browser: Callable[[str], Any] = webbrowser.open,
                    say: Callable[[str], Any] = print, sleep: Callable[[float], Any] = time.sleep,
                    now: Callable[[], float] = time.monotonic, path: Path | None = None) -> PcPass | None:
    """PC の部品が動き出したときに、まだログインしていなければ呼ぶ：ログインが済むまで待ち直し、通行証をしまって返す（止める印が
    立てば None）。ブラウザを開くのは、初めてと、そのあと REOPEN_AFTER ごとまで。"""
    login_once = login_once or login
    opened_at: float | None = None
    while stop is None or not stop.is_set():
        reopen = opened_at is None or now() - opened_at >= REOPEN_AFTER
        try:
            got = login_once(server_url, previous=load(path), open_browser=open_browser if reopen else _quiet,
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
        save(got, path)
        return got
    return None


def _quiet(_: str) -> None:
    """開き直さない回は、ブラウザも案内も出さない（前に開いたページで待っている）。"""


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
    except ValueError:   # 答えが JSON でない
        raise LoginFailed("サーバーの答えが読めません") from None
