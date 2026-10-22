"""中央の見守り役の命令（サーバーで使う。手順は app/README.md）。持ち主 CL-2。

- `python -m salescall.supervisor run`：中央の見守り役を動かす（PC の部品のつなぎ口も開く＝SALESCALL_LISTEN）。
- `python -m salescall.supervisor status`：どの設定が入っているか（値は出さない）。
- `python -m salescall.supervisor pc-pass <契約者の番号> [PC の名前]`：**移る間の形だけ**（サーバーができるまで、管理者の PC で中央の
  見守り役と PC の部品を両方動かす＝DESIGN.md 5-5）。この PC の通行証を払い出して、この PC にしまう（値は出さない。PC の部品は
  それでつなぐ）。サーバーへ移ったあとは、PC の部品のログイン（ブラウザ。CL-3 の入口）で受け取る。
契約者の PC で動かすのは、PC の部品（`python -m salescall.agent`）。
"""

from __future__ import annotations

import socket
import sys
from uuid import UUID

from salescall.core import agents
from salescall.core.access import Access
from salescall.link import pass_file
from salescall.supervisor import ACTOR, config, main


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
    if command == "pc-pass":
        return _pc_pass(argv[1:])
    print("使い方：python -m salescall.supervisor [run | status | pc-pass <契約者の番号> [PC の名前]]")
    return 2


def _pc_pass(args: list[str]) -> int:
    """移る間の形：この PC の通行証を払い出して、この PC にしまう（同じ PC の前の通行証は止める）。"""
    try:
        tenant = UUID(args[0])
    except (IndexError, ValueError):
        print("契約者の番号を入れてください（例：python -m salescall.supervisor pc-pass 00000000-0000-0000-0000-000000000000）")
        return 2
    try:
        cfg = config.load()
    except config.ConfigMissing as e:
        print(str(e))
        return 1
    previous = pass_file.load()
    got = agents.issue(Access(cfg.dsn), ACTOR, tenant, label=args[1] if len(args) > 1 else socket.gethostname(),
                       replaces=previous.pc_id if previous is not None and previous.tenant_id == tenant else None)
    pass_file.save(got)
    print("この PC の通行証を払い出して、しまいました（値は出しません）。PC の部品（python -m salescall.agent run）を動かせます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
