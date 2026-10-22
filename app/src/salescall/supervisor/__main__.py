"""中央の見守り役の命令（サーバーで使う。手順は app/README.md）。持ち主 CL-2。

- `python -m salescall.supervisor run`：中央の見守り役を動かす（PC の部品のつなぎ口も開く＝SALESCALL_LISTEN）。
- `python -m salescall.supervisor status`：どの設定が入っているか（値は出さない）。
契約者の PC で動かすのは、PC の部品（`python -m salescall.agent`）。
"""

from __future__ import annotations

import os
import sys
import threading
import time

from salescall.supervisor import config, main


RESTART_WAIT = 30   # 思いがけない誤りのあと、動き直すまでの長さ（秒）
# 見守り役の流れの名前の頭（決まった間隔の仕事・控え・回線ごとの流れ・つなぎ口。main.py・channel.py の名前と同じ）。
# データベースのつなぎの流れ（salescall-worker など）は数えない（処理の終わりを止めないため）
FLOW_PREFIXES = ("salescall-job", "salescall-backup", "salescall-line-", "salescall-channel")


def left_over() -> list[str]:
    """前の回の見守り役の流れのうち、まだ生きているもの（固まった仕事・止まらない回線の流れなど）の名前。"""
    return sorted(t.name for t in threading.enumerate() if t.is_alive() and t.name.startswith(FLOW_PREFIXES))


def _end(code: int) -> int:
    """終わるとき：前の回の流れが残っていれば、待たずに処理を終える（固まった仕事を Python が待ち続け、処理が終わらないため。
    ⛔ 2026-10-22 の見直し13）。残っていなければ、ふつうに終わる。"""
    if left_over():
        sys.stdout.flush()
        os._exit(code)
    return code


def run(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "status":
        print("\n".join(config.status()))
        return 0
    if command == "run":
        while True:
            try:
                main.serve(config.load())
                return _end(0)
            except (config.ConfigMissing, main.AlreadyRunning) as e:
                print(str(e))
                return _end(1)
            except KeyboardInterrupt:
                return _end(0)
            except Exception as e:  # noqa: BLE001 思いがけない誤りでも、見守り役を終わらせない（間をあけて動き直す＝PC の部品と同じ）
                if left_over():   # 前の回の流れが残っている：同じ処理の中で動き直すと、まだ通話中の流れの回線を後始末して二重に
                    #               立ち上げうる（⛔ 2026-10-22 の見直し13）⇒ 処理を終え、動かす所の仕組み（タスク スケジューラ・
                    #               Fly.io）に立ち上げ直させる
                    print(f"思いがけない誤り（{type(e).__name__}）。前の回の流れが残っているので終わります（自動で立ち上げ直されます）")
                    return _end(1)
                print(f"思いがけない誤り（{type(e).__name__}）。しばらくして動き直します")
                time.sleep(RESTART_WAIT)
    print("使い方：python -m salescall.supervisor [run | status]")
    return 2


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
