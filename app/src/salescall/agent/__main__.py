"""PC の部品の命令（契約者の PC で使う。手順は docs/PC_SETUP.md）。持ち主 CL-2。

- `python -m salescall.agent run`：PC の部品を動かす（ログオンのときに起動する）。まだ SalesCall AI にログインしていない・通行証が
  止められたなら、ブラウザでログインのページを開き、ログインが済むまで待ってから動き出す（契約者は命令を打たない）。
- `status`：どの設定が入っているか（値は出さない）。
- `check-key`：Proton Pass の文字を貼ると、PC に入れた鍵の文字と同じかだけを答える（値は出さない）。
- `login`・`logout`：PC のログインを手でし直す・しまった通行証を消す（管理者が直すとき用）。
- `emergency stop "理由" | status | release "理由"`：本番の PC の上の緊急停止（agent.emergency）。
"""

from __future__ import annotations

import asyncio
import getpass
import sys
import time

from salescall.agent import config, emergency, main, pc_login
from salescall.link import sealing
from salescall.link.locks import AlreadyRunning, SingleInstance, lock_dir

UPDATE_WAIT = 30 * 60   # 版が違うと断られたら、つなぎ直すまで待つ長さ（秒。更新が済むのを待つ）
RESTART_WAIT = 30       # 思いがけない誤りのあと、動き直すまでの長さ（秒）


def run(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "status":
        print("\n".join(config.status() + [f"この PC のログイン：{'済んでいます' if pc_login.load() else 'まだです'}"]))
        return 0
    if command == "check-key":
        text = getpass.getpass("Proton Pass の控えの鍵の文字を貼り付けて Enter（画面には出ません）：")
        try:
            same = sealing.key_matches(text, config.gather())
        except sealing.KeyMissing as e:
            print(str(e))
            return 1
        print("一致しました。PC の鍵は、Proton Pass の文字と同じです。" if same else
              "違います。PC に入れた文字を、Proton Pass からもう一度コピーして貼り直してください。")
        return 0 if same else 1
    if command == "emergency":
        return emergency.main(argv[1:])
    if command in ("login", "logout", "run"):
        try:
            cfg = config.load()
        except config.ConfigMissing as e:
            print(str(e))
            return 1
        if command == "logout":
            print("この PC の通行証を消しました。" if pc_login.logout() else "この PC は、ログインしていません。")
            return 0
        if command == "login":
            try:
                got = pc_login.login(cfg.server_url or "", previous=pc_login.load())
            except pc_login.LoginFailed as e:
                print(str(e))
                return 1
            pc_login.save(got)
            print("この PC のログインが済みました。PC の部品を動かせます。")
            return 0
        try:
            with SingleInstance(lock_dir() / "salescall-agent.lock"):
                return _serve(cfg)
        except AlreadyRunning as e:
            print(str(e))
            return 1
    print("使い方：python -m salescall.agent [run | status | login | logout | check-key | emergency …]")
    return 2


def _serve(cfg: config.Config) -> int:
    while True:
        got = pc_login.load()
        if got is None:
            got = pc_login.until_logged_in(cfg.server_url or "")
            if got is None:
                return 1
        try:
            asyncio.run(main.Agent(cfg, got).run(asyncio.Event()))
            return 0
        except main.NeedLogin:
            if pc_login.load() == got:   # 止められた通行証を消して、ログインし直す（管理者が新しく入れた通行証は消さない）
                pc_login.logout()
        except main.NeedUpdate as e:
            print(f"{e}（更新が済むまで待ちます）")
            time.sleep(UPDATE_WAIT)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 思いがけない誤りでも、PC の部品を終わらせない（間をあけて動き直す）
            print(f"思いがけない誤り（{type(e).__name__}）。しばらくして動き直します")
            time.sleep(RESTART_WAIT)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
