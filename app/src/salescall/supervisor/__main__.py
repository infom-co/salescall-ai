"""見守り役の命令（PC で使う。手順は docs/PC_SETUP.md）。

- `python -m salescall.supervisor run`：見守り役を動かす（ログインのときに起動する）。
- `python -m salescall.supervisor status`：どの設定が入っているか（値は出さない）。
- `python -m salescall.supervisor check-key`：Proton Pass の文字を貼ると、PC に入れた鍵の文字と同じかだけを答える（値は出さない）。
"""

from __future__ import annotations

import getpass
import sys
import threading

from salescall.core import backup
from salescall.supervisor import config, line, main


def run(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "status":
        print("\n".join(config.status()))
        return 0
    if command == "check-key":
        text = getpass.getpass("Proton Pass の控えの鍵の文字を貼り付けて Enter（画面には出ません）：")
        try:
            same = backup.key_matches(text, config.gather())
        except backup.KeyMissing as e:
            print(str(e))
            return 1
        print("一致しました。PC の鍵は、Proton Pass の文字と同じです。" if same else
              "違います。PC に入れた文字を、Proton Pass からもう一度コピーして貼り直してください。")
        return 0 if same else 1
    if command == "line" and len(argv) == 3:   # 回線ごとの処理（本体が立ち上げる。入力が閉じたら、いまの通話のあと止まる）
        stop = threading.Event()
        threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
        line.run(config.gather(), argv[1], argv[2], stop)
        return 0
    if command == "run":
        try:
            main.serve(config.load())
        except (config.ConfigMissing, main.AlreadyRunning) as e:
            print(str(e))
            return 1
        return 0
    print("使い方：python -m salescall.supervisor [run | status | check-key]")
    return 2


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
