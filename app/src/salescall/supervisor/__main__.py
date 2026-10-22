"""中央の見守り役の命令（サーバーで使う。手順は app/README.md）。持ち主 CL-2。

- `python -m salescall.supervisor run`：中央の見守り役を動かす（PC の部品のつなぎ口も開く＝SALESCALL_LISTEN）。
- `python -m salescall.supervisor status`：どの設定が入っているか（値は出さない）。
契約者の PC で動かすのは、PC の部品（`python -m salescall.agent`）。
"""

from __future__ import annotations

import sys

from salescall.supervisor import config, main


def run(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "status":
        print("\n".join(config.status()))
        return 0
    if command == "run":
        try:
            main.serve(config.load())
        except (config.ConfigMissing, main.AlreadyRunning) as e:
            print(str(e))
            return 1
        return 0
    print("使い方：python -m salescall.supervisor [run | status]")
    return 2


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
