"""中央の見守り役の命令（サーバーで使う。手順は app/README.md）。持ち主 CL-2。

- `python -m salescall.supervisor run`：中央の見守り役を動かす（PC の部品のつなぎ口も開く＝SALESCALL_LISTEN）。
- `python -m salescall.supervisor status`：どの設定が入っているか（値は出さない）。
契約者の PC で動かすのは、PC の部品（`python -m salescall.agent`）。
"""

from __future__ import annotations

import sys
import time

from salescall.supervisor import config, main


RESTART_WAIT = 30   # 思いがけない誤りのあと、動き直すまでの長さ（秒）


def run(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "status":
        print("\n".join(config.status()))
        return 0
    if command == "run":
        while True:
            try:
                main.serve(config.load())
                return 0
            except (config.ConfigMissing, main.AlreadyRunning) as e:
                print(str(e))
                return 1
            except KeyboardInterrupt:
                return 0
            except Exception as e:  # noqa: BLE001 思いがけない誤りでも、見守り役を終わらせない（間をあけて動き直す＝PC の部品と同じ）
                print(f"思いがけない誤り（{type(e).__name__}）。しばらくして動き直します")
                time.sleep(RESTART_WAIT)
    print("使い方：python -m salescall.supervisor [run | status]")
    return 2


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
